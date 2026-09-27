from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.operations.routes import CartLine, ProductInput, priced_line


def test_box_preset_uses_pack_price_and_deducts_individual_items():
    product = SimpleNamespace(
        stock_unit="piece",
        price_minor=500,
        cost_minor=300,
        presets=[{"name": "Box of 12", "quantity": 12, "price_minor": 5400}],
    )
    line = CartLine(product_id=uuid4(), quantity=2, preset_index=0)
    assert priced_line(product, line) == (24, 5400, 10800, 7200, "Box of 12")


def test_weight_preset_and_custom_grams_share_gram_stock():
    product = SimpleNamespace(
        stock_unit="gram",
        price_minor=20000,
        cost_minor=12000,
        presets=[{"name": "25 kg bag", "quantity": 25000, "price_minor": 480000}],
    )
    product_id = uuid4()
    assert priced_line(product, CartLine(product_id=product_id, quantity=1, preset_index=0)) == (
        25000, 480000, 480000, 300000, "25 kg bag"
    )
    assert priced_line(product, CartLine(product_id=product_id, quantity=2, grams=500)) == (
        1000, 10000, 20000, 12000, "500 g"
    )
    assert priced_line(product, CartLine(product_id=product_id, quantity=1)) == (
        1000, 20000, 20000, 12000, "kg"
    )


def test_invalid_preset_and_weight_are_rejected():
    product = SimpleNamespace(stock_unit="piece", price_minor=500, cost_minor=300, presets=[])
    with pytest.raises(HTTPException):
        priced_line(product, CartLine(product_id=uuid4(), quantity=1, preset_index=0))
    with pytest.raises(HTTPException):
        priced_line(product, CartLine(product_id=uuid4(), quantity=1, grams=500))
    with pytest.raises(ValueError):
        ProductInput(
            name="Tea", sku="TEA", price_minor=500,
            presets=[
                {"name": "Box", "quantity": 12, "price_minor": 5400},
                {"name": "box", "quantity": 24, "price_minor": 10000},
            ],
        )
