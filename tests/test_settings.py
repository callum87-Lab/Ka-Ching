"""Layout, card order and other saved settings."""


def test_layout_ignores_unknown_cards(client):
    r = client.post("/settings/layout", json={"hidden": ["dash.week", "not.a.card", 5]})
    assert r.json()["hidden"] == ["dash.week"]


def test_card_order_rejects_unknown_page(client):
    assert client.post("/settings/card-order", json={"path": "/orders", "order": ["dash.week"]}).status_code == 400


def test_card_order_saves_and_resets(client):
    assert client.post("/settings/card-order", json={"path": "/", "order": ["dash.trend", "dash.rings"]}).json()["order"] == ["dash.trend", "dash.rings"]
    assert client.post("/settings/card-order", json={"path": "/", "order": []}).json()["order"] == []


def test_wide_layout_choice(client):
    client.post("/settings/wide-layout", data={"layout": "columns"})
    assert 'class="wide-columns"' in client.get("/").text
    client.post("/settings/wide-layout", data={"layout": "nonsense"})
    assert 'class="wide-standard"' in client.get("/").text
