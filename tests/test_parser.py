"""Pieces of the paste-in importer."""
from app import parser


def test_prices():
    assert parser.parse_price("£3.99") == 3.99
    assert parser.parse_price("$12.50") == 12.5
    assert parser.parse_price("1,234.56") == 1234.56
    assert parser.parse_price("no price") is None


def test_dates():
    assert str(parser.parse_date("12 Nov 2026")) == "2026-11-12"
    assert parser.parse_date("nonsense") is None


def test_statuses():
    assert parser.classify_status("Dispatched") == "dispatched"
    assert parser.classify_status("Cancelled") == "cancelled"


def test_generic_parser_finds_each_item_and_price():
    text = "Star Wars: Test Comic #1 (Cover A)\nQty: 1\n£3.99\nBatman: Another Book #2\nQty: 1\n£4.50\nSubtotal £8.49\n"
    items = parser.parse_generic_order(text)["items"]
    assert [i["price"] for i in items] == [3.99, 4.50]
