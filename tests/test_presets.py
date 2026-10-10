"""v3.4: What you track presets (Settings -> Appearance)."""
import json
import sqlite3

from app import core
from .conftest import add_item, set_setting


def _hidden(path):
    row = sqlite3.connect(path).execute("SELECT value FROM settings WHERE key = 'hidden_cards'").fetchone()
    return set(json.loads(row[0])) if row else set()


def test_preset_rules():
    assert core.preset_hidden([]) == set()
    assert core.preset_hidden(["comics"]) == set()
    assert core.preset_hidden(["cards"]) == {"page.creep"}
    assert core.preset_hidden(["figures"]) == {"page.creep", "titles.compare"}
    assert core.preset_hidden(["general"]) == {"page.creep", "page.titles", "titles.series", "titles.compare"}
    assert core.preset_hidden(["comics", "figures"]) == set()
    nopre = core.preset_hidden(["comics"], preorder=False)
    assert {"dash.week", "ov.ratio"} <= nopre


def test_saving_types_hides_price_creep_and_keeps_other_toggles(client, fresh_db):
    set_setting(fresh_db, "hidden_cards", json.dumps(["cal.heatmap"]))
    client.post("/settings/tracking", data={"kinds": ["cards"], "preorder": "yes", "action": "save"})
    assert _hidden(fresh_db) == {"cal.heatmap", "page.creep"}
    page = client.get("/insights").text
    assert 'href="/insights/price-creep"' not in page and ">Top sets</a>" in page
    # the page itself still opens if you go there
    assert client.get("/insights/price-creep").status_code == 200


def test_reset_puts_every_card_back_to_the_preset(client, fresh_db):
    set_setting(fresh_db, "hidden_cards", json.dumps(["cal.heatmap", "ov.dist"]))
    client.post("/settings/tracking", data={"kinds": ["comics"], "preorder": "no", "action": "reset"})
    assert _hidden(fresh_db) == core.preset_hidden(["comics"], preorder=False)


def test_card_shows_changes_and_reset_button(client, fresh_db):
    client.post("/settings/tracking", data={"kinds": ["comics", "cards"], "preorder": "yes"})
    page = client.get("/settings").text
    assert "Preset: <b>Comics + Trading cards</b>" in page and "Reset cards to preset" not in page
    set_setting(fresh_db, "hidden_cards", json.dumps(["ov.dist"]))
    page = client.get("/settings").text
    assert "1 card changed by you" in page and "Reset cards to preset" in page


def test_unsaved_kinds_come_from_categories_in_use(client, fresh_db):
    cards = sqlite3.connect(fresh_db).execute("SELECT id FROM categories WHERE name = 'Pokémon cards'").fetchone()[0]
    add_item(fresh_db, "Booster", 4.5, 5, category=cards)
    with client:
        page = client.get("/settings").text
    assert 'value="cards" checked' in page and 'value="comics" checked' not in page


def test_old_layout_presets_are_gone(client, fresh_db):
    page = client.get("/settings").text
    assert 'data-preset="nopre"' not in page and 'id="tracking-card"' in page
